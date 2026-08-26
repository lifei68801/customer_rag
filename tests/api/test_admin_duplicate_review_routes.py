from __future__ import annotations

import asyncio
import json

import aiosqlite
import pytest
from fastapi.testclient import TestClient

from app.api import deps
from app.api.admin_session import AdminSessionStore
from app.config.settings import Settings
from app.graphrag.duplicate_review_queue import (
    enqueue_duplicate_suggestion,
    ensure_duplicate_review_schema,
)
from app.graphrag.ontology import Term
from app.graphrag.ontology_lifecycle import ensure_ontology_schema
from app.graphrag.tenants_store import create_tenant, create_tenants_table
from app.graphrag.terms_store import ensure_terms_schema
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


async def _open_review_conn() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    await ensure_duplicate_review_schema(conn)
    await ensure_terms_schema(conn)
    # 路由在 approve/reject 之前会先用 review_conn 调 require_active_tenant()
    # 校验 payload.tenant_id——真实的 deps.get_review_conn() 会自动建好
    # tenants 表并回填历史租户，这里是手工建表的测试连接，绕开了那条路径，
    # 必须显式建表 + 注册本文件所有用例用到的 tenant_id（"demo"）。
    await create_tenants_table(conn)
    await create_tenant(conn, tenant_id="demo", name="demo")
    # approve_duplicate_suggestion() 最终经 terms_store.update_term() 校验
    # term_type 是否在该租户"已确认"的分类白名单里（_validate_categories）
    # ——需要 ontology_term_types 表存在，并且本文件用到的 term_type（"公司"）
    # 有一条 status='confirmed' 的行，否则会报 UnknownCategoryError。
    await ensure_ontology_schema(conn)
    return conn


async def _seed_confirmed_term_type(
    conn: aiosqlite.Connection, *, tenant_id: str, term_type: str
) -> None:
    """直接往 ontology_term_types 插入一条 status='confirmed' 的行——测试
    只关心 approve_duplicate_suggestion()->update_term() 能查到这个分类
    已确认，不需要走完整的草稿编辑+confirm_ontology 生命周期。同 sister
    文件 test_admin_graph_review_routes.py 的 _seed_confirmed_ontology()。
    """
    await conn.execute(
        "INSERT INTO ontology_term_types (tenant_id, value, extra_fields, status) "
        "VALUES (?, ?, '[]', 'confirmed')",
        (tenant_id, term_type),
    )
    await conn.commit()


@pytest.fixture
def review_conn():
    """duplicate-review 队列库连接。必须显式 close：aiosqlite 的后台工作
    线程不是 daemon 线程，泄漏一个未关闭的连接会让 pytest 进程在跑完全部
    用例后卡在解释器退出阶段（threading._shutdown 等这个线程），表现为
    "测试全绿但命令不返回"。做法同 test_admin_graph_review_routes.py 的
    review_conn fixture。
    """
    conn = asyncio.run(_open_review_conn())
    try:
        yield conn
    finally:
        asyncio.run(conn.close())


async def _seed_terms(conn: aiosqlite.Connection, terms: list[Term]) -> None:
    """approve 路由最终会经 duplicate_review_queue.approve_duplicate_suggestion
    读 terms 表做别名合并——直接往 terms 表插行，绕开 create_term() 的分类
    校验和图谱同步，这里只关心路由/合并逻辑查到了正确的术语。"""
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


def test_list_and_approve_duplicate_suggestion(review_conn):
    asyncio.run(
        _seed_confirmed_term_type(review_conn, tenant_id="demo", term_type="公司")
    )
    asyncio.run(
        _seed_terms(
            review_conn,
            [
                Term(
                    tenant_id="demo", node_key="公司:Coca-Cola", standard_name="Coca-Cola",
                    aliases=[], term_type="公司",
                ),
                Term(
                    tenant_id="demo", node_key="公司:可口可乐", standard_name="可口可乐",
                    aliases=["Coca-Cola"], term_type="公司",
                ),
            ],
        )
    )
    # 照抄 test_admin_graph_review_routes.py 的模式：不通过 HTTP 走完整的
    # 创建术语流程（那条路径还要求 graph_client 依赖，会额外拉起 Neo4j
    # 客户端），而是直接用 enqueue_duplicate_suggestion() 往测试用的
    # review_conn 里插入一条待审核记录，同一个连接随后通过
    # app.dependency_overrides[deps.get_review_conn] 注入给 TestClient，
    # 这样断言时读到的和路由查询到的是同一份数据。
    asyncio.run(
        enqueue_duplicate_suggestion(
            review_conn,
            tenant_id="demo",
            candidate_a_node_key="公司:Coca-Cola",
            candidate_b_node_key="公司:可口可乐",
            similarity_score=0.92,
            reason="alias_overlap",
        )
    )
    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_review_conn] = lambda: review_conn
    try:
        client = TestClient(app)
        headers = _authed_headers(session_store)
        response = client.get(
            "/api/admin/duplicate-reviews", params={"tenant_id": "demo"}, headers=headers,
        )
        assert response.status_code == 200
        body = response.json()
        assert body["total"] >= 1
        review_id = body["suggestions"][0]["review_id"]
        keep_node_key = body["suggestions"][0]["candidate_a_node_key"]

        approve_response = client.post(
            f"/api/admin/duplicate-reviews/{review_id}/approve",
            json={"tenant_id": "demo", "keep_node_key": keep_node_key},
            headers=headers,
        )
    finally:
        app.dependency_overrides.clear()

    assert approve_response.status_code == 200
    assert approve_response.json() == {"approved": True}


def test_reject_duplicate_suggestion(review_conn):
    # 同上，先预置一条 pending 记录，再调 reject 端点，断言 200 + {"rejected": True}。
    asyncio.run(
        enqueue_duplicate_suggestion(
            review_conn,
            tenant_id="demo",
            candidate_a_node_key="公司:Coca-Cola",
            candidate_b_node_key="公司:可口可乐",
            similarity_score=0.92,
            reason="alias_overlap",
        )
    )
    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_review_conn] = lambda: review_conn
    try:
        client = TestClient(app)
        headers = _authed_headers(session_store)
        list_response = client.get(
            "/api/admin/duplicate-reviews", params={"tenant_id": "demo"}, headers=headers,
        )
        assert list_response.status_code == 200
        review_id = list_response.json()["suggestions"][0]["review_id"]

        reject_response = client.post(
            f"/api/admin/duplicate-reviews/{review_id}/reject",
            json={"tenant_id": "demo", "note": "误判，不是重复项"},
            headers=headers,
        )
    finally:
        app.dependency_overrides.clear()

    assert reject_response.status_code == 200
    assert reject_response.json() == {"rejected": True}


def test_approve_unknown_review_id_returns_404(review_conn):
    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_review_conn] = lambda: review_conn
    try:
        client = TestClient(app)
        response = client.post(
            "/api/admin/duplicate-reviews/999999/approve",
            json={"tenant_id": "demo", "keep_node_key": "x"},
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 404
