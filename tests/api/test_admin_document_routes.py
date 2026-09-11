import asyncio
import io
import json

import aiosqlite
import pytest
from fastapi.testclient import TestClient

from app.auth.admin_users_store import create_admin_user, ensure_admin_users_schema
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
        subject_node_key,
        object_node_key,
        relation_type,
        source,
        tenant_id,
        provenance,
        recorded_at,
    ) -> None:
        self.written.append(
            {
                "subject": subject_node_key,
                "object": object_node_key,
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
    await create_term_type(conn, tenant_id, value="error_code", actor="alice")
    await create_term_type(conn, tenant_id, value="module", actor="alice")
    await add_allowed_combination(
        conn, tenant_id,
        subject_term_type="error_code", relation_type="RELATED_TO", object_term_type="module",
    actor="alice")
    await confirm_ontology(conn, tenant_id, actor="alice")


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
    # require_admin_session 现在每个请求都要确认账号仍是 active，
    # 所以本体库里必须有这张表和一个可用的管理员。
    await ensure_admin_users_schema(conn)
    await create_admin_user(
        conn, username="admin", password="password1", role="admin", tenant_id=None
    )
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
    token = session_store.create_session(username="admin", role="admin", tenant_id=None)
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


# ---------------------------------------------------------------------------
# 批量删除
#
# 文档列表**有分页、没有筛选**（GET /documents 只认 page/page_size），所以
# 两档是「本页 20 条」和「整租户的全部」——filters 传空对象就是后者。任务
# 列表（pending_jobs/dead_jobs）既不分页也不筛选，只有一档。
#
# 下面每一组批量用例里的批次都**同时包含能删的和删不掉的**：全能删或全删
# 不掉的批次，「逐条收集失败」和「一律当成功」两种实现都能变绿。
# ---------------------------------------------------------------------------


class _VectorStoreFailingOnOneSource(InMemoryVectorStore):
    """指定的那一份文档删向量时炸，其余照常。用来在一个批次里同时造出
    能删的和删不掉的。"""

    def __init__(self, failing_source: str) -> None:
        super().__init__()
        self._failing_source = failing_source

    async def delete_by_source(self, *, source: str, tenant_id: str) -> None:
        if source == self._failing_source:
            raise RuntimeError("milvus 连接失败")
        await super().delete_by_source(source=source, tenant_id=tenant_id)


def _bulk_delete_documents(
    ingestion_conn, review_conn, upload_dir, payload, *, vector_store=None, tenant_id="t1"
):
    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_ingestion_conn] = lambda: ingestion_conn
    app.dependency_overrides[deps.get_vector_store] = lambda: (
        vector_store if vector_store is not None else InMemoryVectorStore()
    )
    app.dependency_overrides[deps.get_upload_dir] = lambda: upload_dir
    app.dependency_overrides[deps.get_review_conn] = lambda: review_conn
    try:
        client = TestClient(app)
        return client.post(
            f"/api/admin/{tenant_id}/documents/bulk-delete",
            json=payload,
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()


def _seed_documents(ingestion_conn, paths: list[str], *, tenant_id: str = "t1") -> None:
    async def _run() -> None:
        for i, path in enumerate(paths):
            await record_ingested(
                ingestion_conn, tenant_id=tenant_id, file_path=path,
                content_hash=f"h{i}", chunk_count=1,
            )

    asyncio.run(_run())


def test_bulk_delete_documents_by_paths_deletes_only_the_listed_ones(
    tmp_path, ingestion_conn, review_conn
):
    """本页全选：只删传进来的那几条，同一租户下别的文档不受影响。

    批次里故意混进一条不存在的路径——它必须被点名报出来而不是算进成功数，
    否则用户会以为拼错的那个路径对应的文档已经清掉了。
    """
    _seed_documents(ingestion_conn, ["a.md", "b.md", "c.md", "d.md", "e.md"])

    response = _bulk_delete_documents(
        ingestion_conn, review_conn, tmp_path / "uploads",
        {"file_paths": ["a.md", "b.md", "不存在.md"]},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["requested"] == 3
    assert body["deleted"] == 2
    assert [f["key"] for f in body["failures"]] == ["不存在.md"]
    # 没列进来的三条还在——「删本页」不能变成「删全部」。
    assert sorted(asyncio.run(_tracked_paths(ingestion_conn, "t1"))) == ["c.md", "d.md", "e.md"]


def test_bulk_delete_documents_by_empty_filters_covers_the_whole_tenant_not_one_page(
    tmp_path, ingestion_conn, review_conn
):
    """「全部」= 整租户的全部，不是列表当前那一页。

    这里故意放 23 条（超过界面上那一页的 20 条）：把「全部」实现成"再拉
    一页"的话，剩下的 3 条会静默留下来，而用户以为清干净了。

    其中一条删向量会失败：能删的删掉、删不掉的逐条报出来，且失败那条的
    追踪记录必须还在——批次里全能删的话，这条断言钉不住任何东西。
    """
    paths = [f"doc{i:02d}.md" for i in range(23)]
    _seed_documents(ingestion_conn, paths)

    response = _bulk_delete_documents(
        ingestion_conn, review_conn, tmp_path / "uploads", {"filters": {}},
        vector_store=_VectorStoreFailingOnOneSource("doc07.md"),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["requested"] == 23
    assert body["deleted"] == 22
    assert [f["key"] for f in body["failures"]] == ["doc07.md"]
    assert "向量" in body["failures"][0]["reason"]
    # 失败的那条确实还在，成功的确实没了。
    assert asyncio.run(_tracked_paths(ingestion_conn, "t1")) == ["doc07.md"]


def test_bulk_delete_documents_never_reaches_another_tenants_documents(
    tmp_path, ingestion_conn, review_conn
):
    """整租户的「全部」也只是这一个租户的全部。"""
    _seed_documents(ingestion_conn, ["mine-a.md", "mine-b.md"])
    _seed_documents(ingestion_conn, ["theirs.md"], tenant_id="t2")

    response = _bulk_delete_documents(
        ingestion_conn, review_conn, tmp_path / "uploads", {"filters": {}},
        vector_store=_VectorStoreFailingOnOneSource("mine-b.md"),
    )

    assert response.status_code == 200, response.text
    assert response.json()["deleted"] == 1
    assert asyncio.run(_tracked_paths(ingestion_conn, "t2")) == ["theirs.md"]


def test_bulk_delete_documents_unlinks_each_file_as_it_goes_and_does_not_roll_back(
    tmp_path, ingestion_conn, review_conn
):
    """删掉的那几条的磁盘文件当场就没了，不因为同批里有一条失败而"回滚"
    ——文件删了就是删了，假装还能撤销比如实报告更危险。"""
    upload_dir = tmp_path / "uploads"
    tenant_dir = upload_dir / "t1"
    tenant_dir.mkdir(parents=True)
    files = []
    for name in ("abc_a.md", "abc_b.md", "abc_c.md"):
        f = tenant_dir / name
        f.write_text("内容", encoding="utf-8")
        files.append(f)
    _seed_documents(ingestion_conn, [str(f) for f in files])

    response = _bulk_delete_documents(
        ingestion_conn, review_conn, upload_dir,
        {"file_paths": [str(f) for f in files]},
        vector_store=_VectorStoreFailingOnOneSource(str(files[1])),
    )

    assert response.status_code == 200, response.text
    assert response.json()["deleted"] == 2
    assert not files[0].exists()
    assert not files[2].exists()
    # 删不掉的那条：向量都没清成，磁盘文件更不该动。
    assert files[1].exists()


def test_bulk_delete_documents_with_both_modes_returns_400(tmp_path, ingestion_conn, review_conn):
    """「删这 1 条」和「删整租户的全部」差两个数量级，接口不猜。"""
    _seed_documents(ingestion_conn, ["a.md", "b.md"])

    response = _bulk_delete_documents(
        ingestion_conn, review_conn, tmp_path / "uploads",
        {"file_paths": ["a.md"], "filters": {}},
    )

    assert response.status_code == 400
    assert sorted(asyncio.run(_tracked_paths(ingestion_conn, "t1"))) == ["a.md", "b.md"]


def test_bulk_delete_documents_with_neither_mode_returns_400(tmp_path, ingestion_conn, review_conn):
    _seed_documents(ingestion_conn, ["a.md", "b.md"])

    response = _bulk_delete_documents(ingestion_conn, review_conn, tmp_path / "uploads", {})

    assert response.status_code == 400
    assert sorted(asyncio.run(_tracked_paths(ingestion_conn, "t1"))) == ["a.md", "b.md"]


def _bulk_delete_jobs(
    ingestion_conn, review_conn, upload_dir, payload, *, vector_store=None, tenant_id="t1"
):
    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_ingestion_conn] = lambda: ingestion_conn
    app.dependency_overrides[deps.get_vector_store] = lambda: (
        vector_store if vector_store is not None else InMemoryVectorStore()
    )
    app.dependency_overrides[deps.get_upload_dir] = lambda: upload_dir
    app.dependency_overrides[deps.get_review_conn] = lambda: review_conn
    try:
        client = TestClient(app)
        return client.post(
            f"/api/admin/{tenant_id}/documents/jobs/bulk-delete",
            json=payload,
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()


def _enqueue_job(ingestion_conn, file_path: str, *, dead: bool, tenant_id: str = "t1") -> str:
    from app.ingestion.ingestion_queue import enqueue_ingestion_job, mark_job_failed

    async def _run() -> str:
        job_id = await enqueue_ingestion_job(
            ingestion_conn, tenant_id=tenant_id, file_path=file_path,
            content_hash="h1", action="ingest",
        )
        if dead:
            await mark_job_failed(ingestion_conn, job_id, error="解析失败", max_attempts=1)
        return job_id

    return asyncio.run(_run())


def _remaining_job_ids(ingestion_conn, tenant_id: str = "t1") -> list[str]:
    async def _run() -> list[str]:
        cursor = await ingestion_conn.execute(
            "SELECT job_id FROM ingestion_jobs WHERE tenant_id = ?", (tenant_id,)
        )
        return sorted(row[0] for row in await cursor.fetchall())

    return asyncio.run(_run())


def test_bulk_delete_jobs_deletes_the_dead_ones_and_names_the_ones_it_cannot(
    tmp_path, ingestion_conn, review_conn
):
    """批次里混着能删的（失败任务）和删不掉的（还在正常排队的任务）：
    能删的删掉，删不掉的逐条报出来并说明为什么。

    正在排队的任务当场删掉是危险的——它可能正在被处理。单条路径为此回
    409，批量路径要把同一条判据保留成逐条的失败明细，而不是为了"批量"
    把它放宽掉。
    """
    dead_a = _enqueue_job(ingestion_conn, "a.md", dead=True)
    alive = _enqueue_job(ingestion_conn, "b.md", dead=False)
    dead_c = _enqueue_job(ingestion_conn, "c.md", dead=True)

    response = _bulk_delete_jobs(
        ingestion_conn, review_conn, tmp_path / "uploads",
        {"job_ids": [dead_a, alive, dead_c]},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["requested"] == 3
    assert body["deleted"] == 2
    assert [f["key"] for f in body["failures"]] == [alive]
    assert "无法删除" in body["failures"][0]["reason"]
    # 删不掉的那条确实还在，能删的确实没了。
    assert _remaining_job_ids(ingestion_conn) == [alive]


def test_bulk_delete_jobs_reports_unknown_ids_instead_of_counting_them_as_deleted(
    tmp_path, ingestion_conn, review_conn
):
    dead = _enqueue_job(ingestion_conn, "a.md", dead=True)

    response = _bulk_delete_jobs(
        ingestion_conn, review_conn, tmp_path / "uploads",
        {"job_ids": [dead, "根本不存在的任务"]},
    )

    body = response.json()
    assert body["deleted"] == 1
    assert [f["key"] for f in body["failures"]] == ["根本不存在的任务"]


def test_bulk_delete_jobs_cleans_up_each_files_and_chunks_as_it_goes(
    tmp_path, ingestion_conn, review_conn
):
    """删任务的连带清理（上传文件 + 孤儿 chunk）在批量里要逐条真的发生。

    批次里有一条删不掉（还在排队），它的文件和 chunk 必须原样留着；已经
    清掉的那条不会因为它而"回滚"。
    """
    upload_dir = tmp_path / "uploads"
    tenant_dir = upload_dir / "t1"
    tenant_dir.mkdir(parents=True)
    files = {}
    for name in ("abc_a.md", "abc_b.md"):
        f = tenant_dir / name
        f.write_text("内容", encoding="utf-8")
        files[name] = f
    dead = _enqueue_job(ingestion_conn, str(files["abc_a.md"]), dead=True)
    alive = _enqueue_job(ingestion_conn, str(files["abc_b.md"]), dead=False)

    vector_store = InMemoryVectorStore()
    asyncio.run(
        vector_store.upsert(
            [
                VectorRecord(
                    id=f"{files[name]}#0", vector=[0.1, 0.2], text="写了一半的内容",
                    tenant_id="t1", metadata={"source": str(files[name])},
                )
                for name in files
            ]
        )
    )

    response = _bulk_delete_jobs(
        ingestion_conn, review_conn, upload_dir,
        {"job_ids": [dead, alive]}, vector_store=vector_store,
    )

    assert response.status_code == 200, response.text
    assert response.json()["deleted"] == 1
    assert not files["abc_a.md"].exists()
    assert files["abc_b.md"].exists()
    remaining = asyncio.run(
        vector_store.search(query_vector=[0.1, 0.2], top_k=10, tenant_id="t1")
    )
    assert [r.metadata["source"] for r in remaining] == [str(files["abc_b.md"])]


def test_bulk_delete_jobs_never_reaches_another_tenants_job(
    tmp_path, ingestion_conn, review_conn
):
    """别的租户的任务 id 就算被猜到也删不掉——它在这个租户里等同于不存在。"""
    mine = _enqueue_job(ingestion_conn, "a.md", dead=True)
    theirs = _enqueue_job(ingestion_conn, "b.md", dead=True, tenant_id="t2")

    response = _bulk_delete_jobs(
        ingestion_conn, review_conn, tmp_path / "uploads", {"job_ids": [mine, theirs]},
    )

    body = response.json()
    assert body["deleted"] == 1
    assert [f["key"] for f in body["failures"]] == [theirs]
    assert _remaining_job_ids(ingestion_conn, "t2") == [theirs]


def test_bulk_delete_jobs_without_ids_returns_400(tmp_path, ingestion_conn, review_conn):
    """任务列表没有分页也没有筛选，只有「选中的这些」一档——但"一个都没给"
    仍然是调用方的 bug，不能被当成"那就全删了吧"。"""
    dead = _enqueue_job(ingestion_conn, "a.md", dead=True)

    response = _bulk_delete_jobs(ingestion_conn, review_conn, tmp_path / "uploads", {})

    assert response.status_code == 400
    assert _remaining_job_ids(ingestion_conn) == [dead]
