import asyncio
import io
import json

import aiosqlite
import pytest
from fastapi.testclient import TestClient

from app.api import deps
from app.api.admin_session import AdminSessionStore
from app.graphrag.ontology import Term
from app.graphrag.review_queue import ensure_review_schema
from app.graphrag.tenants_store import create_tenant, create_tenants_table
from app.graphrag.term_edits_store import ensure_term_edits_schema
from app.graphrag.terms_store import ensure_terms_schema
from app.ingestion.ingestion_queue import ensure_ingestion_queue_schema
from app.ingestion.tracking import ensure_tracking_schema, record_ingested
from app.main import app
from app.providers.base import ProviderCapability, ProviderRequest, ProviderResult
from app.providers.embedding import EmbeddingRegistry, EmbeddingRequest, EmbeddingResult
from app.providers.registry import ProviderRegistry
from app.retrieval.vector_store import InMemoryVectorStore, VectorRecord
from tests.settings_factory import build_settings


class FakeEmbeddingProvider:
    async def embed(self, request: EmbeddingRequest) -> EmbeddingResult:
        return EmbeddingResult(vectors=[[0.1, 0.2] for _ in request.texts])


class FixedLLMProvider:
    """图谱抽取用的假 LLM：不管输入是什么都返回同一段候选关系 JSON。"""

    def __init__(self, text: str) -> None:
        self._text = text

    async def complete(self, request: ProviderRequest) -> ProviderResult:
        return ProviderResult(text=self._text)


class SpyGraphClient:
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
        provenance,
        recorded_at,
    ) -> None:
        self.written.append(
            {
                "subject": subject_standard_name,
                "object": object_standard_name,
                "relation_type": relation_type,
                "tenant_id": tenant_id,
                "provenance": provenance,
            }
        )

    async def delete_relations_by_source(self, source: str, *, tenant_id: str) -> None:
        self.deleted_sources.append((source, tenant_id))


_TENANT_ID = "t1"

_TERMS = [
    Term(
        tenant_id=_TENANT_ID,
        node_key="示例错误码E502",
        standard_name="示例错误码E502",
        aliases=["网关超时示例"],
        term_type="error_code",
    ),
    Term(
        tenant_id=_TENANT_ID,
        node_key="示例登录模块",
        standard_name="示例登录模块",
        aliases=["示例认证模块"],
        term_type="module",
    ),
]

_RESOLVABLE_RELATION_JSON = (
    '{"relations": [{"subject": "网关超时示例", '
    '"object": "示例认证模块", "relation_type": "RELATED_TO", '
    '"subject_type": "error_code", "object_type": "module"}]}'
)


async def _confirm_error_code_module_related_to_ontology(
    conn: aiosqlite.Connection, tenant_id: str = "t1"
) -> None:
    """把 conn 上该租户的本体 schema 建到"已确认"状态——_maybe_extract_
    graph_relations 现在会先查 is_ontology_confirmed()（未确认则跳过图谱
    抽取），再查 status="confirmed" 的关系类型/实体类型/允许组合传给
    extract_and_write_graph_relations()。默认接入模式（extraction）下
    checkout_draft() 会播种 10 种通用关系类型（含本文件用到的
    RELATED_TO），额外补上 error_code/module 两种实体类型和它们之间的
    RELATED_TO 允许组合，再一并确认——这是 _RESOLVABLE_RELATION_JSON 这条
    候选关系（网关超时示例[error_code] --RELATED_TO--> 示例认证模块
    [module]）能被放行、写进图谱所需的最小 schema。
    """
    from app.graphrag.ontology_categories import create_term_type
    from app.graphrag.ontology_constraints import add_allowed_combination
    from app.graphrag.ontology_lifecycle import checkout_draft, confirm_ontology, ensure_ontology_schema

    await ensure_ontology_schema(conn)
    await checkout_draft(conn, tenant_id)
    await create_term_type(conn, tenant_id, value="error_code")
    await create_term_type(conn, tenant_id, value="module")
    await add_allowed_combination(
        conn, tenant_id,
        subject_term_type="error_code", relation_type="RELATED_TO", object_term_type="module",
    )
    await confirm_ontology(conn, tenant_id)


def _llm_registry_returning(text: str) -> ProviderRegistry:
    registry = ProviderRegistry()
    registry.register(
        ProviderCapability.LLM, deps.DEFAULT_LLM_PROVIDER_NAME, FixedLLMProvider(text)
    )
    return registry


def _settings(**overrides):
    return build_settings(**{"admin_token": "tok", **overrides})


async def _open_ingestion_conn() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    await ensure_tracking_schema(conn)
    await ensure_ingestion_queue_schema(conn)
    return conn


@pytest.fixture
def ingestion_conn():
    """摄取库连接。必须显式 close：aiosqlite 的后台工作线程不是 daemon
    线程，泄漏一个未关闭的连接会让 pytest 进程在跑完全部用例后卡在解释器
    退出阶段（threading._shutdown 等这个线程），表现为"测试全绿但命令不返回"。
    """
    conn = asyncio.run(_open_ingestion_conn())
    try:
        yield conn
    finally:
        asyncio.run(conn.close())


async def _open_review_conn() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    await ensure_review_schema(conn)
    # get_review_conn（app/api/deps.py）在生产环境里同一个连接同时建
    # review_queue 和 terms 两套 schema——upload_document/retry_ingestion_job
    # 现在不再经 deps.get_terms，而是直接用自己拿到的 review_conn 调
    # list_terms_merged()（Task 3 改道），测试用的连接必须跟生产环境一样
    # 把 terms + term_edits 两套 schema 都建好，否则 list_terms_merged 会报
    # "no such table: terms" / "no such table: term_edits"。
    await ensure_terms_schema(conn)
    await ensure_term_edits_schema(conn)
    # Task 4：这个文件里的写接口（upload_document/delete_document/
    # retry_ingestion_job/delete_ingestion_job）现在都会先用 review_conn 调
    # require_active_tenant() 校验 tenant_id——真实的 deps.get_review_conn()
    # 会自动建好 tenants 表并回填历史租户，但这里是手工建表的测试连接，
    # 绕开了那条路径，必须显式建表 + 注册本文件测试里用到的 tenant_id
    # （"t1"，本文件所有写接口调用都用这个值），否则校验会因为表不存在
    # 报底层 SQL 错误，或者查不到租户返回假的 404。
    await create_tenants_table(conn)
    await create_tenant(conn, tenant_id="t1", name="t1")
    return conn


@pytest.fixture
def review_conn():
    """图谱人工审核队列连接。close 的理由同 ingestion_conn。"""
    conn = asyncio.run(_open_review_conn())
    try:
        yield conn
    finally:
        asyncio.run(conn.close())


async def _seed_terms(conn: aiosqlite.Connection, terms: list[Term]) -> None:
    """直接按 terms 表结构写行，绕开 create_term() 的分类校验——这里的
    测试只关心"upload_document/retry_ingestion_job 路由用自己解析的
    tenant_id 查到了正确的术语"，不关心分类枚举表是否也注册过。
    """
    for term in terms:
        await conn.execute(
            "INSERT OR REPLACE INTO terms "
            "(tenant_id, node_key, standard_name, aliases, term_type, "
            "extra_properties) VALUES (?, ?, ?, ?, ?, ?)",
            (
                term.tenant_id,
                term.node_key,
                term.standard_name,
                json.dumps(term.aliases, ensure_ascii=False),
                term.term_type,
                json.dumps(term.extra_properties, ensure_ascii=False),
            ),
        )
    await conn.commit()


def _authed_headers(session_store: AdminSessionStore) -> dict[str, str]:
    token = session_store.create_session()
    return {"Authorization": f"Bearer {token}"}


def _upload_overrides(
    session_store,
    ingestion_conn,
    upload_dir,
    *,
    vector_store=None,
    llm_registry=None,
    graph_client=None,
    review_conn=None,
    terms=None,
) -> None:
    """上传接口依赖的全部 provider 覆盖。

    图谱那几项（llm_registry/graph_client/review_conn）现在是上传
    路由的无条件依赖（build_graph 是逐任务判断的，资源必须先备好），
    不覆盖的话测试会去真建 Neo4j driver、真开仓库里的 SQLite 文件。

    terms 不再通过 deps.get_terms 覆盖注入（Fix 3：upload_document/
    retry_ingestion_job 改成直接用自己的 tenant_id 从 review_conn 里
    查 terms 表）——调用方不传 review_conn 时这里用一个全新的、已建好
    schema 的空连接兜底；传了 terms 参数时把它们写进这个连接的 terms
    表，路由内部真的查出这些数据，而不是靠 mock 短路。
    """
    resolved_review_conn = review_conn if review_conn is not None else asyncio.run(_open_review_conn())
    if terms is not None:
        asyncio.run(_seed_terms(resolved_review_conn, terms))
    embedding_registry = EmbeddingRegistry()
    embedding_registry.register(deps.DEFAULT_EMBEDDING_PROVIDER_NAME, FakeEmbeddingProvider())
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_ingestion_conn] = lambda: ingestion_conn
    app.dependency_overrides[deps.get_embedding_registry] = lambda: embedding_registry
    app.dependency_overrides[deps.get_vector_store] = lambda: vector_store or InMemoryVectorStore()
    app.dependency_overrides[deps.get_upload_dir] = lambda: upload_dir
    app.dependency_overrides[deps.get_llm_registry] = lambda: (
        llm_registry or _llm_registry_returning('{"relations": []}')
    )
    app.dependency_overrides[deps.get_graph_client] = lambda: (
        graph_client if graph_client is not None else SpyGraphClient()
    )
    app.dependency_overrides[deps.get_review_conn] = lambda: resolved_review_conn


def test_upload_without_session_returns_401():
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    try:
        client = TestClient(app)
        response = client.post(
            "/api/admin/t1/documents",
            files={"file": ("a.md", b"## t\ncontent", "text/markdown")},
            data={"build_graph": "false"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 401


def test_upload_rejects_file_larger_than_100mb(tmp_path, ingestion_conn):
    session_store = AdminSessionStore()
    upload_dir = tmp_path / "uploads"
    _upload_overrides(session_store, ingestion_conn, upload_dir)
    try:
        client = TestClient(app)
        oversized = io.BytesIO(b"0" * (101 * 1024 * 1024))
        response = client.post(
            "/api/admin/t1/documents",
            files={"file": ("big.md", oversized, "text/markdown")},
            data={"build_graph": "false"},
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 413


def test_upload_enqueues_job_and_returns_job_id(tmp_path, ingestion_conn):
    session_store = AdminSessionStore()
    upload_dir = tmp_path / "uploads"
    _upload_overrides(session_store, ingestion_conn, upload_dir)
    try:
        client = TestClient(app)
        response = client.post(
            "/api/admin/t1/documents",
            files={"file": ("a.md", b"## t\ncontent", "text/markdown")},
            data={"build_graph": "false"},
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert "job_id" in response.json()
    # 落盘路径是 <upload_dir>/<tenant_id>/<uuid>_<原文件名>，所以要递归 glob。
    assert (upload_dir / "t1").is_dir()
    assert len(list(upload_dir.rglob("*a.md"))) == 1


def test_upload_sanitizes_traversal_in_filename(tmp_path, ingestion_conn):
    """文件名里的 ../ 不能让文件落到 upload_dir 之外。"""
    session_store = AdminSessionStore()
    root = tmp_path / "root"
    upload_dir = root / "uploads"
    upload_dir.mkdir(parents=True)
    _upload_overrides(session_store, ingestion_conn, upload_dir)
    try:
        client = TestClient(app)
        response = client.post(
            "/api/admin/t1/documents",
            files={"file": ("../../pwned.md", b"## t\ncontent", "text/markdown")},
            data={"build_graph": "false"},
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    # 文件确实落在了消毒后的租户目录里，而不是"没报错"而已。
    landed = list((upload_dir / "t1").iterdir())
    assert len(landed) == 1
    assert landed[0].name.endswith(".._.._pwned.md")
    # upload_dir 之外（它的父目录）没有多出任何东西。
    assert [p.name for p in root.iterdir()] == ["uploads"]


def test_upload_rejects_tenant_id_with_path_separators(tmp_path, ingestion_conn):
    """tenant_id 里的路径分隔符要被 400 拒掉，且不能创建任何目录。"""
    session_store = AdminSessionStore()
    root = tmp_path / "root"
    upload_dir = root / "uploads"
    upload_dir.mkdir(parents=True)
    _upload_overrides(session_store, ingestion_conn, upload_dir)
    try:
        client = TestClient(app)
        # tenant_id 现在是一个路径段，含 "/" 的值在结构上就到不了这个路由
        # ——连编码过的 %2F 也一样（实测 Starlette 仍按分隔符处理）。这比
        # 应用层的 400 更强：请求根本没进来。
        response = client.post(
            "/api/admin/%2E%2E%2F%2E%2E%2Fpwned/documents",
            files={"file": ("a.md", b"## t\ncontent", "text/markdown")},
            data={"build_graph": "false"},
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 404
    assert list(upload_dir.iterdir()) == []
    assert [p.name for p in root.iterdir()] == ["uploads"]


def test_upload_rejects_dot_only_tenant_id(tmp_path, ingestion_conn):
    """纯点的 tenant_id（"." / ".."）也不合法——它会指向 upload_dir 自身或父目录。"""
    session_store = AdminSessionStore()
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir(parents=True)
    _upload_overrides(session_store, ingestion_conn, upload_dir)
    try:
        client = TestClient(app)
        # 未编码的 ".." 会被 URL 规范化掉（打不到路由），编码成 %2E%2E 就能
        # 穿透到应用层——这一层必须由 _validate_tenant_id 挡住，不能只靠
        # URL 规范化。
        response = client.post(
            "/api/admin/%2E%2E/documents",
            files={"file": ("a.md", b"## t\ncontent", "text/markdown")},
            data={"build_graph": "false"},
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 400
    assert list(upload_dir.iterdir()) == []


def test_list_documents_returns_tracked_files_for_tenant(ingestion_conn):
    asyncio.run(
        record_ingested(
            ingestion_conn, tenant_id="t1", file_path="a.md", content_hash="h1", chunk_count=3
        )
    )
    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_ingestion_conn] = lambda: ingestion_conn
    try:
        client = TestClient(app)
        response = client.get(
            "/api/admin/t1/documents",
            
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body["documents"][0]["file_path"] == "a.md"
    assert body["documents"][0]["chunk_count"] == 3


def test_list_documents_paginates_with_page_and_page_size(ingestion_conn):
    """种 3 条追踪记录（显式设置互不相同的 last_ingested_at，避免同一秒
    落点导致 ORDER BY 打平——理由同 tests/ingestion/test_tracking.py 的
    _seed_with_explicit_timestamps），GET ?page=2&page_size=1 应该只返回
    按 last_ingested_at DESC 排序后的第 2 条（file1.md），total 字段反映
    该租户全部追踪记录数（3），不受当前这一页大小的影响。"""

    async def _seed() -> None:
        for index, timestamp in enumerate(
            ["2024-01-01T00:00:00", "2024-01-02T00:00:00", "2024-01-03T00:00:00"]
        ):
            await ingestion_conn.execute(
                "INSERT INTO ingested_documents "
                "(tenant_id, file_path, content_hash, chunk_count, last_ingested_at) "
                "VALUES (?, ?, ?, ?, ?)",
                ("t1", f"file{index}.md", f"h{index}", 1, timestamp),
            )
        await ingestion_conn.commit()

    asyncio.run(_seed())
    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_ingestion_conn] = lambda: ingestion_conn
    try:
        client = TestClient(app)
        response = client.get(
            "/api/admin/t1/documents",
            params={"page": 2, "page_size": 1},
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert [d["file_path"] for d in body["documents"]] == ["file1.md"]
    assert body["total"] == 3


def test_list_documents_without_page_params_returns_full_list_beyond_default_page_size(
    ingestion_conn,
):
    """回归测试：list_documents 的 page/page_size 曾经默认为 1/20（跟
    Task 8 修复前的 list_all_terms 是同一类 bug），会把不传分页参数的裸
    GET 悄悄截断成只有第一页。这里种 21 条追踪记录，不传 page/page_size
    请求，断言拿到的是全部 21 条而不是被截断的 20 条，且和 total 字段
    一致。"""

    async def _seed() -> None:
        for i in range(21):
            await ingestion_conn.execute(
                "INSERT INTO ingested_documents "
                "(tenant_id, file_path, content_hash, chunk_count, last_ingested_at) "
                "VALUES (?, ?, ?, ?, ?)",
                ("t1", f"file{i:02d}.md", f"h{i}", 1, f"2024-01-{i + 1:02d}T00:00:00"),
            )
        await ingestion_conn.commit()

    asyncio.run(_seed())
    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_ingestion_conn] = lambda: ingestion_conn
    try:
        client = TestClient(app)
        response = client.get(
            "/api/admin/t1/documents",
            
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert len(body["documents"]) == 21
    assert len(body["documents"]) > 20
    assert body["total"] == 21
    assert len(body["documents"]) == body["total"]


def test_list_documents_excludes_other_tenants_pending_jobs(ingestion_conn):
    from app.ingestion.ingestion_queue import enqueue_ingestion_job

    async def _seed() -> None:
        await enqueue_ingestion_job(
            ingestion_conn, tenant_id="t1", file_path="mine.md",
            content_hash="h1", action="ingest",
        )
        await enqueue_ingestion_job(
            ingestion_conn, tenant_id="t2", file_path="theirs.md",
            content_hash="h2", action="ingest",
        )

    asyncio.run(_seed())
    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_ingestion_conn] = lambda: ingestion_conn
    try:
        client = TestClient(app)
        response = client.get(
            "/api/admin/t1/documents",
            
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    pending = response.json()["pending_jobs"]
    assert [job["file_path"] for job in pending] == ["mine.md"]


def test_delete_document_removes_tracking_and_vectors(tmp_path, ingestion_conn, review_conn):
    asyncio.run(
        record_ingested(
            ingestion_conn, tenant_id="t1", file_path="a.md", content_hash="h1", chunk_count=1
        )
    )
    vector_store = InMemoryVectorStore()
    asyncio.run(
        vector_store.upsert(
            [
                VectorRecord(
                    id="a.md#0", vector=[0.1, 0.2], text="内容",
                    tenant_id="t1", metadata={"source": "a.md"},
                )
            ]
        )
    )

    session_store = AdminSessionStore()
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_ingestion_conn] = lambda: ingestion_conn
    app.dependency_overrides[deps.get_vector_store] = lambda: vector_store
    app.dependency_overrides[deps.get_upload_dir] = lambda: upload_dir
    app.dependency_overrides[deps.get_review_conn] = lambda: review_conn
    try:
        client = TestClient(app)
        response = client.request(
            "DELETE",
            "/api/admin/t1/documents",
            params={"file_path": "a.md"},
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    remaining = asyncio.run(
        vector_store.search(query_vector=[0.1, 0.2], top_k=10, tenant_id="t1")
    )
    assert remaining == []
    assert asyncio.run(_tracked_paths(ingestion_conn, "t1")) == []


def test_delete_document_also_unlinks_uploaded_file(tmp_path, ingestion_conn, review_conn):
    """删除文档要把 data/uploads 下的原始文件也删掉，不能只清索引。"""
    upload_dir = tmp_path / "uploads"
    tenant_dir = upload_dir / "t1"
    tenant_dir.mkdir(parents=True)
    uploaded = tenant_dir / "abc_a.md"
    uploaded.write_text("# t\n内容", encoding="utf-8")
    asyncio.run(
        record_ingested(
            ingestion_conn, tenant_id="t1", file_path=str(uploaded),
            content_hash="h1", chunk_count=1,
        )
    )

    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_ingestion_conn] = lambda: ingestion_conn
    app.dependency_overrides[deps.get_vector_store] = lambda: InMemoryVectorStore()
    app.dependency_overrides[deps.get_upload_dir] = lambda: upload_dir
    app.dependency_overrides[deps.get_review_conn] = lambda: review_conn
    try:
        client = TestClient(app)
        response = client.request(
            "DELETE",
            "/api/admin/t1/documents",
            params={"file_path": str(uploaded)},
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert not uploaded.exists()


def test_delete_document_returns_502_with_clear_message_when_vector_store_fails(
    tmp_path, ingestion_conn, review_conn
):
    """向量库删除失败时要返回带明确信息的 502，而不是裸 500。"""
    asyncio.run(
        record_ingested(
            ingestion_conn, tenant_id="t1", file_path="a.md", content_hash="h1", chunk_count=1
        )
    )

    class FailingVectorStore(InMemoryVectorStore):
        async def delete_by_source(self, *, source: str, tenant_id: str) -> None:
            raise RuntimeError("milvus 连接失败")

    session_store = AdminSessionStore()
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_ingestion_conn] = lambda: ingestion_conn
    app.dependency_overrides[deps.get_vector_store] = lambda: FailingVectorStore()
    app.dependency_overrides[deps.get_upload_dir] = lambda: upload_dir
    app.dependency_overrides[deps.get_review_conn] = lambda: review_conn
    try:
        client = TestClient(app)
        response = client.request(
            "DELETE",
            "/api/admin/t1/documents",
            params={"file_path": "a.md"},
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 502
    assert "删除向量数据失败" in response.json()["detail"]
    # 向量库那一步失败，追踪记录不应该被清理，避免"向量还在但追踪记录没了"
    assert asyncio.run(_tracked_paths(ingestion_conn, "t1")) == ["a.md"]


def test_delete_document_returns_502_when_tracking_cleanup_fails_after_vector_delete(
    tmp_path, ingestion_conn, review_conn, monkeypatch
):
    """追踪记录清理失败时也要给出明确的 502，并提示已产生的不一致状态。"""
    asyncio.run(
        record_ingested(
            ingestion_conn, tenant_id="t1", file_path="a.md", content_hash="h1", chunk_count=1
        )
    )

    async def _boom(*args, **kwargs):
        raise RuntimeError("数据库锁住了")

    import app.api.admin_document_routes as routes_module

    monkeypatch.setattr(routes_module, "remove_tracked_file", _boom)

    session_store = AdminSessionStore()
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_ingestion_conn] = lambda: ingestion_conn
    app.dependency_overrides[deps.get_vector_store] = lambda: InMemoryVectorStore()
    app.dependency_overrides[deps.get_upload_dir] = lambda: upload_dir
    app.dependency_overrides[deps.get_review_conn] = lambda: review_conn
    try:
        client = TestClient(app)
        response = client.request(
            "DELETE",
            "/api/admin/t1/documents",
            params={"file_path": "a.md"},
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 502
    assert "可能需要手动核实" in response.json()["detail"]


def test_delete_document_keeps_files_outside_upload_dir(tmp_path, ingestion_conn, review_conn):
    """CLI 摄取的原始语料不在 upload_dir 里，后台删除只清索引，不能删用户的文件。"""
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    outside = tmp_path / "corpus" / "a.md"
    outside.parent.mkdir()
    outside.write_text("# t\n内容", encoding="utf-8")
    asyncio.run(
        record_ingested(
            ingestion_conn, tenant_id="t1", file_path=str(outside),
            content_hash="h1", chunk_count=1,
        )
    )

    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_ingestion_conn] = lambda: ingestion_conn
    app.dependency_overrides[deps.get_vector_store] = lambda: InMemoryVectorStore()
    app.dependency_overrides[deps.get_upload_dir] = lambda: upload_dir
    app.dependency_overrides[deps.get_review_conn] = lambda: review_conn
    try:
        client = TestClient(app)
        response = client.request(
            "DELETE",
            "/api/admin/t1/documents",
            params={"file_path": str(outside)},
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert outside.exists()


def test_upload_rejects_unsupported_file_type(tmp_path, ingestion_conn):
    """摄取管线不支持的扩展名要同步 400，不落盘、不入队。"""
    session_store = AdminSessionStore()
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    _upload_overrides(session_store, ingestion_conn, upload_dir)
    try:
        client = TestClient(app)
        response = client.post(
            "/api/admin/t1/documents",
            files={"file": ("payload.exe", b"MZ\x00\x00", "application/octet-stream")},
            data={"build_graph": "false"},
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 400
    assert list(upload_dir.iterdir()) == []
    assert asyncio.run(_pending_job_paths(ingestion_conn)) == []


def test_upload_rejects_tenant_id_outside_milvus_charset(tmp_path, ingestion_conn):
    """能通过旧的 Unicode 宽松校验、但过不了 Milvus 严格白名单的 tenant_id
    必须在入口就 400——否则请求拿到 200 + job_id、文件已落盘，然后在后台
    任务/DELETE 里才炸。
    """
    session_store = AdminSessionStore()
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    _upload_overrides(session_store, ingestion_conn, upload_dir)
    try:
        client = TestClient(app)
        response = client.post(
            "/api/admin/租户.一/documents",
            files={"file": ("a.md", b"## t\ncontent", "text/markdown")},
            data={"build_graph": "false"},
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 400
    assert list(upload_dir.iterdir()) == []
    assert asyncio.run(_pending_job_paths(ingestion_conn)) == []


def test_upload_with_build_graph_true_runs_graph_extraction(
    tmp_path, ingestion_conn, review_conn
):
    """build_graph=true 的正面路径：上传接口必须把图谱资源一路传到
    process_pending_jobs()，后台任务真的走到 LLM 抽取 + 写图谱那一步。
    """
    session_store = AdminSessionStore()
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    graph_client = SpyGraphClient()
    asyncio.run(_confirm_error_code_module_related_to_ontology(review_conn))
    _upload_overrides(
        session_store,
        ingestion_conn,
        upload_dir,
        llm_registry=_llm_registry_returning(_RESOLVABLE_RELATION_JSON),
        graph_client=graph_client,
        review_conn=review_conn,
        terms=_TERMS,
    )
    try:
        client = TestClient(app)
        response = client.post(
            "/api/admin/t1/documents",
            files={
                "file": (
                    "a.md",
                    "# 标题\n网关超时示例通常与示例认证模块相关".encode("utf-8"),
                    "text/markdown",
                )
            },
            data={"build_graph": "true"},
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    # BackgroundTasks 在 TestClient 返回之前就跑完了，所以这里能直接断言结果。
    assert graph_client.deleted_sources, "图谱抽取根本没被触发"
    assert [
        (item["subject"], item["object"], item["relation_type"], item["tenant_id"])
        for item in graph_client.written
    ] == [("示例错误码E502", "示例登录模块", "RELATED_TO", "t1")]


def test_upload_with_build_graph_false_skips_graph_extraction(
    tmp_path, ingestion_conn, review_conn
):
    """反面对照：图谱资源同样传了，但这条任务没勾建图，就不该碰图谱。"""
    session_store = AdminSessionStore()
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    graph_client = SpyGraphClient()
    _upload_overrides(
        session_store,
        ingestion_conn,
        upload_dir,
        llm_registry=_llm_registry_returning(_RESOLVABLE_RELATION_JSON),
        graph_client=graph_client,
        review_conn=review_conn,
        terms=_TERMS,
    )
    try:
        client = TestClient(app)
        response = client.post(
            "/api/admin/t1/documents",
            files={
                "file": (
                    "a.md",
                    "# 标题\n网关超时示例通常与示例认证模块相关".encode("utf-8"),
                    "text/markdown",
                )
            },
            data={"build_graph": "false"},
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert graph_client.written == []
    assert graph_client.deleted_sources == []


async def _pending_job_paths(conn: aiosqlite.Connection) -> list[str]:
    from app.ingestion.ingestion_queue import list_pending_jobs

    return [job["file_path"] for job in await list_pending_jobs(conn, limit=50)]


async def _tracked_paths(conn: aiosqlite.Connection, tenant_id: str) -> list[str]:
    from app.ingestion.tracking import list_tracked_files

    rows = await list_tracked_files(conn, tenant_id=tenant_id)
    return [row["file_path"] for row in rows]


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
            "/api/admin/t1/documents", 
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert len(body["dead_jobs"]) == 1
    assert body["dead_jobs"][0]["job_id"] == job_id
    assert body["dead_jobs"][0]["last_error"] == "解析失败"


def test_retry_job_resets_to_pending_and_returns_200(tmp_path, ingestion_conn, review_conn):
    from app.ingestion.ingestion_queue import (
        enqueue_ingestion_job,
        list_pending_jobs,
        mark_job_failed,
    )

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
    app.dependency_overrides[deps.get_graph_client] = lambda: SpyGraphClient()
    # retry_ingestion_job 现在直接用自己解析的 tenant_id 从 review_conn 查
    # terms 表（Fix 3），不再经 deps.get_terms——传一个真实建过 schema 的
    # 连接，而不是 None，否则路由内部的 list_terms() 会直接崩掉。
    app.dependency_overrides[deps.get_review_conn] = lambda: review_conn
    try:
        client = TestClient(app)
        response = client.post(
            f"/api/admin/t1/documents/jobs/{job_id}/retry",
            
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    # a.md 实际不存在磁盘上，所以下面两个断言一起证明了两件事：(1) retry_job()
    # 真的把 dead 重置回了 pending（不是仍然 dead 或者被删掉了），(2)
    # background_tasks.add_task(_run_pending_jobs, ...) 真的被调用并且
    # TestClient 同步跑完了它——如果这次重试处理从没被触发，attempts 会
    # 停在 retry_job() 刚重置时的 0，而不是变成 1（一次失败的处理尝试）。
    # 重试耗尽变 dead 的行为本身已经在 tests/ingestion/test_ingestion_queue.py
    # 里覆盖过，这里不重复断言那一层。
    pending = asyncio.run(list_pending_jobs(ingestion_conn, tenant_id="t1"))
    assert [j["job_id"] for j in pending] == [job_id]
    assert pending[0]["attempts"] == 1


def test_retry_job_returns_404_for_unknown_job(ingestion_conn, review_conn):
    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_ingestion_conn] = lambda: ingestion_conn
    app.dependency_overrides[deps.get_embedding_registry] = lambda: EmbeddingRegistry()
    app.dependency_overrides[deps.get_vector_store] = lambda: InMemoryVectorStore()
    app.dependency_overrides[deps.get_llm_registry] = lambda: ProviderRegistry()
    app.dependency_overrides[deps.get_graph_client] = lambda: SpyGraphClient()
    app.dependency_overrides[deps.get_review_conn] = lambda: review_conn
    try:
        client = TestClient(app)
        response = client.post(
            "/api/admin/t1/documents/jobs/unknown-id/retry",
            
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 404


def test_retry_job_returns_409_when_job_is_not_dead(ingestion_conn, review_conn):
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
    app.dependency_overrides[deps.get_graph_client] = lambda: SpyGraphClient()
    app.dependency_overrides[deps.get_review_conn] = lambda: review_conn
    try:
        client = TestClient(app)
        response = client.post(
            f"/api/admin/t1/documents/jobs/{job_id}/retry",
            
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 409


def test_delete_job_removes_it_and_unlinks_orphaned_file(tmp_path, ingestion_conn, review_conn):
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
    app.dependency_overrides[deps.get_vector_store] = lambda: InMemoryVectorStore()
    app.dependency_overrides[deps.get_review_conn] = lambda: review_conn
    try:
        client = TestClient(app)
        response = client.request(
            "DELETE",
            f"/api/admin/t1/documents/jobs/{job_id}",
            
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert not orphaned.exists()


def test_delete_job_removes_orphaned_vector_chunks(tmp_path, ingestion_conn, review_conn):
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

    # 模拟一个在"部分 chunk 已经写进向量库"之后才失败的任务——record_ingested
    # 从没跑过（所以不在已摄取文档列表里），但这些 chunk 已经真实存在于
    # 向量库中，删除失败任务时必须一并清掉。
    vector_store = InMemoryVectorStore()
    asyncio.run(
        vector_store.upsert(
            [
                VectorRecord(
                    id=f"{orphaned}#0", vector=[0.1, 0.2], text="部分写入的内容",
                    tenant_id="t1", metadata={"source": str(orphaned)},
                )
            ]
        )
    )

    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_ingestion_conn] = lambda: ingestion_conn
    app.dependency_overrides[deps.get_upload_dir] = lambda: upload_dir
    app.dependency_overrides[deps.get_vector_store] = lambda: vector_store
    app.dependency_overrides[deps.get_review_conn] = lambda: review_conn
    try:
        client = TestClient(app)
        response = client.request(
            "DELETE",
            f"/api/admin/t1/documents/jobs/{job_id}",
            
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    remaining = asyncio.run(
        vector_store.search(query_vector=[0.1, 0.2], top_k=10, tenant_id="t1")
    )
    assert remaining == []


def test_delete_job_returns_409_when_job_is_not_dead(ingestion_conn, review_conn):
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
    app.dependency_overrides[deps.get_vector_store] = lambda: InMemoryVectorStore()
    app.dependency_overrides[deps.get_review_conn] = lambda: review_conn
    try:
        client = TestClient(app)
        response = client.request(
            "DELETE",
            f"/api/admin/t1/documents/jobs/{job_id}",
            
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 409


def test_delete_document_does_not_unlink_file_under_a_different_tenant_directory(
    tmp_path, ingestion_conn, review_conn
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
    app.dependency_overrides[deps.get_review_conn] = lambda: review_conn
    try:
        client = TestClient(app)
        response = client.request(
            "DELETE",
            "/api/admin/t1/documents",
            params={"file_path": str(other_tenants_file)},
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert other_tenants_file.exists()


def test_delete_document_returns_404_for_unknown_tenant(tmp_path, ingestion_conn, review_conn):
    """Task 4：写接口在做具体业务逻辑之前，要先校验 tenant_id 在 tenants
    注册表里存在且是 active——一个从未注册过的 tenant_id 应该直接 404，
    而不是被当作合法租户走完整个删除流程（哪怕这个租户底下什么记录
    都没有，"删除成功"这个 200 响应本身就是一个误导）。"""
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_ingestion_conn] = lambda: ingestion_conn
    app.dependency_overrides[deps.get_vector_store] = lambda: InMemoryVectorStore()
    app.dependency_overrides[deps.get_upload_dir] = lambda: upload_dir
    app.dependency_overrides[deps.get_review_conn] = lambda: review_conn
    try:
        client = TestClient(app)
        response = client.request(
            "DELETE",
            "/api/admin/no-such-tenant/documents",
            params={"file_path": "a.md"},
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 404


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
            "/api/admin/t1/documents/chunks",
            params={"file_path": "a.md"},
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
            "/api/admin/t1/documents/chunks",
            params={"file_path": "a.md"},
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
            "/api/admin/t1/documents/file",
            params={"file_path": str(the_file)},
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
            "/api/admin/t1/documents/file",
            params={"file_path": str(other_tenants_file)},
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 404
