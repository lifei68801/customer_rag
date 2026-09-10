"""数据库导入端点。

核心承诺是**库里零密码**（spec D3）：建数据源的请求体里出现 password 直接
报错，同步时必须现给。被测数据库用 SQLite，不需要起容器。
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import aiosqlite
import pytest
from fastapi.testclient import TestClient

from app.api import deps
from app.api.admin_session import AdminSessionStore
from app.auth.admin_users_store import create_admin_user
from app.graphrag.db_sources_store import ensure_db_sources_schema, get_db_source
from app.graphrag.etl_skipped_rows import ensure_etl_skipped_rows_schema
from app.graphrag.ontology_categories import create_term_type
from app.graphrag.etl_stable_code_registry import ensure_stable_code_registry_schema
from app.graphrag.ontology_lifecycle import (
    checkout_draft,
    confirm_ontology,
    ensure_ontology_schema,
)
from app.graphrag.ontology_relations import create_relation_type
from app.graphrag.tenants_store import create_tenant, create_tenants_table
from app.graphrag.term_edits_store import ensure_term_edits_schema
from app.graphrag.terms_store import ensure_terms_schema
from app.main import app
from tests.schema_fixtures import ensure_admin_auth_schema
from tests.settings_factory import build_settings


class FakeGraph:
    """接口照 tests/graphrag/test_schema_etl.py 的 FakeGraphClient——ETL 调的是
    那几个方法，少一个就在跑批中途炸。"""

    def __init__(self) -> None:
        self.synced: list[str] = []
        self.deleted_nodes: list[str] = []
        self.stale_sweeps: list[tuple[str, str]] = []

    async def sync_term(self, term) -> None:
        self.synced.append(term.node_key)

    async def merge_relation(self, **kwargs) -> None:
        return None

    async def delete_term_node(self, *, tenant_id: str, node_key: str) -> None:
        self.deleted_nodes.append(node_key)

    async def count_stale_relations_by_source(
        self, source: str, *, tenant_id: str, before_recorded_at: str
    ) -> tuple[int, int]:
        return (0, 0)

    async def delete_stale_relations_by_source(
        self, source: str, *, tenant_id: str, before_recorded_at: str
    ) -> int:
        self.stale_sweeps.append((source, before_recorded_at))
        return 0


@pytest.fixture()
def goods_db(tmp_path: Path) -> Path:
    path = tmp_path / "goods.sqlite"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE goods (id INTEGER, name TEXT)")
    conn.executemany(
        "INSERT INTO goods (id, name) VALUES (?, ?)", [(1, "可乐"), (2, "雪碧")]
    )
    conn.commit()
    conn.close()
    return path


async def _open_conn() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    try:
        await create_tenants_table(conn)
        await ensure_admin_auth_schema(conn)
        await ensure_db_sources_schema(conn)
        await ensure_terms_schema(conn)
        await ensure_term_edits_schema(conn)
        await ensure_ontology_schema(conn)
        await ensure_etl_skipped_rows_schema(conn)
        await ensure_stable_code_registry_schema(conn)
        for tenant in ("demo", "other"):
            await create_tenant(conn, tenant_id=tenant, name=tenant)
            await create_term_type(conn, tenant, value="Product", actor="admin")
            # is_ontology_confirmed 看的是 tenant_relation_types 里有没有
            # confirmed 行，所以至少要有一个关系类型走完"草稿 → 确认"。
            await create_relation_type(
                conn, tenant, relation_type="RELATED_TO",
                example_phrase="Product RELATED_TO Product", actor="admin",
            )
            await confirm_ontology(conn, tenant, actor="admin")
        await create_admin_user(
            conn, username="admin", password="password1", role="admin", tenant_id=None
        )
        await create_admin_user(
            conn, username="member", password="password1", role="member", tenant_id="demo"
        )
    except BaseException:
        await conn.close()
        raise
    return conn


@pytest.fixture()
def db_conn():
    conn = asyncio.run(_open_conn())
    try:
        yield conn
    finally:
        asyncio.run(conn.close())


def _client(db_conn, graph=None, *, username="admin", role="admin", tenant_id=None):
    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: build_settings(admin_token="tok")
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_review_conn] = lambda: db_conn
    app.dependency_overrides[deps.get_graph_client] = lambda: graph or FakeGraph()
    token = session_store.create_session(username=username, role=role, tenant_id=tenant_id)
    return TestClient(app), {"Authorization": f"Bearer {token}"}


def _post(db_conn, path, body, *, graph=None, **who):
    client, headers = _client(db_conn, graph, **who)
    try:
        return client.post(path, json=body, headers=headers)
    finally:
        app.dependency_overrides.clear()


def _get(db_conn, path, **who):
    client, headers = _client(db_conn, **who)
    try:
        return client.get(path, headers=headers)
    finally:
        app.dependency_overrides.clear()


def _delete(db_conn, path, **who):
    client, headers = _client(db_conn, **who)
    try:
        return client.delete(path, headers=headers)
    finally:
        app.dependency_overrides.clear()


def _conn_fields(db: Path) -> dict:
    return {
        "driver": "sqlite",
        "host": "",
        "port": 0,
        "database": str(db),
        "username": "",
    }


_MAPPING = {
    "term_type": "Product",
    "standard_name_parts": ["name"],
    "node_key_parts": [{"column": "id"}],
    # 不声明额外字段：term_type 上没有 extra_fields，映射一个未声明的字段
    # 会让每一行都被跳过。
    "field_mappings": {},
}


def _create_source(db_conn, goods_db: Path, *, extra: dict | None = None, tenant: str = "demo"):
    body = {
        **_conn_fields(goods_db),
        "name": "商品库",
        "query": "SELECT id, name FROM goods",
        "mapping": _MAPPING,
    }
    if extra:
        body.update(extra)
    return _post(db_conn, f"/api/admin/{tenant}/db-import/sources", body)


def test_creating_a_source_rejects_a_request_body_that_carries_a_password(db_conn, goods_db):
    """建数据源的请求体里出现 password 要被拒，不是静默忽略。

    静默忽略的话，前端某次改动不小心把密码发上来我们会一直不知道——而它
    已经进了访问日志。返回 400 还是 422 不是要害（pydantic 的 extra="forbid"
    产出 422），要害是**它没被悄悄吞掉**。
    """
    response = _create_source(db_conn, goods_db, extra={"password": "hunter2"})

    assert response.status_code in (400, 422)
    # 反面：不带 password 的同一个请求必须成功，否则"一律拒绝"的实现也能
    # 让上面那条变绿。
    assert _create_source(db_conn, goods_db).status_code == 200


def test_the_stored_source_has_no_password_anywhere_in_it(db_conn, goods_db):
    """存下来的东西里翻不出密码。"""
    source_id = _create_source(db_conn, goods_db).json()["source_id"]

    stored = asyncio.run(get_db_source(db_conn, tenant_id="demo", source_id=source_id))

    assert stored is not None
    assert "hunter2" not in json.dumps(stored, ensure_ascii=False, default=str)
    assert not any("password" in key.lower() for key in stored)


def test_sync_requires_a_password_in_the_request(db_conn, goods_db):
    """密码不在库里，所以每次同步必须现给。

    缺了要报错而不是拿空密码去连——空密码在某些配置下真的能连上，那会连到
    一个错误的账号下，而用户以为同步的是他配的那个库。
    """
    source_id = _create_source(db_conn, goods_db).json()["source_id"]

    response = _post(db_conn, f"/api/admin/demo/db-import/sources/{source_id}/sync", {})

    assert response.status_code in (400, 422)


def test_preview_returns_columns_and_caps_the_rows(db_conn, goods_db, tmp_path):
    """预览带列名，且最多 100 行。

    不限制的话，「预览一下」会把两千万行拉进内存然后序列化成 JSON。
    """
    big = tmp_path / "big.sqlite"
    conn = sqlite3.connect(big)
    conn.execute("CREATE TABLE goods (id INTEGER, name TEXT)")
    conn.executemany(
        "INSERT INTO goods (id, name) VALUES (?, ?)", [(i, f"n{i}") for i in range(150)]
    )
    conn.commit()
    conn.close()

    body = _post(
        db_conn, "/api/admin/demo/db-import/preview",
        {**_conn_fields(big), "password": "", "query": "SELECT id, name FROM goods"},
    ).json()

    assert body["columns"] == ["id", "name"]
    assert len(body["rows"]) == 100
    # 截断了要说出来，否则用户对着 100 行下「这张表就这么大」的结论。
    assert body["truncated"] is True


def test_preview_does_not_claim_truncation_when_everything_fits(db_conn, goods_db):
    """没截断时 truncated=false。恒为 true 的实现会让每次预览都挂着一句假话。"""
    body = _post(
        db_conn, "/api/admin/demo/db-import/preview",
        {**_conn_fields(goods_db), "password": "", "query": "SELECT id, name FROM goods"},
    ).json()

    assert body["truncated"] is False
    assert body["row_count"] == 2


def test_a_failed_connection_returns_400_with_a_message_that_has_no_password(db_conn):
    """错误消息进响应体、进日志、被截图。密码不能在里面。"""
    response = _post(
        db_conn, "/api/admin/demo/db-import/test-connection",
        {
            "driver": "postgresql", "host": "10.0.0.254", "port": 5432,
            "database": "d", "username": "u", "password": "hunter2",
        },
    )

    assert response.status_code == 400
    assert "hunter2" not in response.text
    # 该说的还是要说清楚：连不上哪里。只剩一句"连接失败"的话，用户不知道
    # 是地址写错了还是网络不通。
    assert "10.0.0.254" in response.text


def test_a_non_select_query_is_refused_by_the_preview_endpoint(db_conn, goods_db):
    """只读判据在端点这一层也生效。"""
    response = _post(
        db_conn, "/api/admin/demo/db-import/preview",
        {**_conn_fields(goods_db), "password": "", "query": "DELETE FROM goods"},
    )

    assert response.status_code == 400


def test_sync_reuses_the_stored_query_and_mapping(db_conn, goods_db):
    """同步用的是存下来的 SQL 和映射，不是请求里现给的。

    允许请求覆盖的话，「重新同步」这个动作的语义就不是「再跑一次同样的」了
    ——而用户点它的时候以为是。
    """
    source_id = _create_source(db_conn, goods_db).json()["source_id"]

    response = _post(
        db_conn, f"/api/admin/demo/db-import/sources/{source_id}/sync",
        {"password": "", "query": "SELECT id FROM goods"},
    )

    # 多带一个 query 字段直接被拒（extra="forbid"）——比"收下但忽略"诚实：
    # 忽略的话用户以为自己换了 SQL，而跑的还是老的。
    assert response.status_code in (400, 422)


def test_sync_records_when_and_how_many(db_conn, goods_db):
    """同步完 last_sync_at 和 last_sync_rows 都要更新。"""
    source_id = _create_source(db_conn, goods_db).json()["source_id"]

    response = _post(
        db_conn, f"/api/admin/demo/db-import/sources/{source_id}/sync", {"password": ""}
    )

    assert response.status_code == 200, response.text
    assert response.json()["row_count"] == 2
    stored = asyncio.run(get_db_source(db_conn, tenant_id="demo", source_id=source_id))
    assert stored is not None
    assert stored["last_sync_rows"] == 2
    assert stored["last_sync_at"]


def test_a_failed_sync_does_not_update_last_sync_at(db_conn, goods_db):
    """失败的同步不该刷新时间戳。

    刷了的话，列表上显示「上次同步 2 分钟前」而实际上那次同步一行都没导进去。
    """
    source_id = _create_source(db_conn, goods_db).json()["source_id"]
    goods_db.unlink()  # 把库删掉，让这次同步必失败

    response = _post(
        db_conn, f"/api/admin/demo/db-import/sources/{source_id}/sync", {"password": ""}
    )

    assert response.status_code == 400
    stored = asyncio.run(get_db_source(db_conn, tenant_id="demo", source_id=source_id))
    assert stored is not None
    assert stored["last_sync_at"] is None


def test_sync_goes_through_the_same_mapping_pipeline_as_sheet_import(db_conn, goods_db):
    """拉到行之后走的是跟表格导入同一条管线。

    同一份校验、同一份报告、同一套跳过行记录。另写一条的话，同样的坏数据在
    两个入口下会有两种表现，而用户会拿这两处互相印证。
    """
    source_id = _create_source(db_conn, goods_db).json()["source_id"]
    graph = FakeGraph()

    body = _post(
        db_conn, f"/api/admin/demo/db-import/sources/{source_id}/sync",
        {"password": ""}, graph=graph,
    ).json()

    # 实体真的写进了术语表和图谱——不是"接口返回了 200"。
    assert body["entities_written"] == 2
    assert sorted(graph.synced) == ["Product:1", "Product:2"]


def test_sync_records_bad_rows_in_the_shared_skipped_rows_table(db_conn, goods_db, tmp_path):
    """同步跳掉的行进的是**跟表格导入同一张**跳过行表。

    自己另记一份的话，报错明细页的「表格跳行」页看不到数据库导入跳的行——
    而用户不会觉得那是两回事。
    """
    from app.graphrag.etl_skipped_rows import list_skipped_rows

    dirty = tmp_path / "dirty.sqlite"
    conn = sqlite3.connect(dirty)
    conn.execute("CREATE TABLE goods (id INTEGER, name TEXT)")
    conn.executemany(
        "INSERT INTO goods (id, name) VALUES (?, ?)", [(1, "可乐"), (None, "没有ID的")]
    )
    conn.commit()
    conn.close()

    body = {
        **_conn_fields(dirty),
        "name": "脏商品库",
        "query": "SELECT id, name FROM goods",
        "mapping": _MAPPING,
    }
    source_id = _post(db_conn, "/api/admin/demo/db-import/sources", body).json()["source_id"]

    _post(db_conn, f"/api/admin/demo/db-import/sources/{source_id}/sync", {"password": ""})

    rows = asyncio.run(list_skipped_rows(db_conn, tenant_id="demo"))
    assert len(rows) == 1
    assert rows[0]["run_id"].startswith("db-sync-")


def test_listing_shows_never_synced_as_empty_not_zero(db_conn, goods_db):
    """从没同步过时两个字段都是空。

    编一个 0 出来的话，列表上会显示「刚刚同步 · 0 行」——而它一次都没跑过，
    那是两件完全不同的事。
    """
    _create_source(db_conn, goods_db)

    items = _get(db_conn, "/api/admin/demo/db-import/sources").json()["items"]

    assert len(items) == 1
    assert items[0]["last_sync_at"] is None
    assert items[0]["last_sync_rows"] is None


def test_deleting_a_source_removes_it_from_the_list(db_conn, goods_db):
    source_id = _create_source(db_conn, goods_db).json()["source_id"]

    assert _delete(db_conn, f"/api/admin/demo/db-import/sources/{source_id}").status_code == 200

    assert _get(db_conn, "/api/admin/demo/db-import/sources").json()["items"] == []


@pytest.mark.parametrize(
    "method,path",
    [
        ("post", "/api/admin/other/db-import/test-connection"),
        ("post", "/api/admin/other/db-import/preview"),
        ("post", "/api/admin/other/db-import/sources"),
        ("get", "/api/admin/other/db-import/sources"),
        ("post", "/api/admin/other/db-import/sources/x/sync"),
        ("delete", "/api/admin/other/db-import/sources/x"),
    ],
)
def test_every_endpoint_is_tenant_scoped(db_conn, method: str, path: str):
    """六个端点逐个断言 403。

    漏挂的那一个在生产上不会有任何报错，请求照常成功，只是操作了别人的
    数据源——而那里面有别人内网数据库的地址和账号。
    """
    who = {"username": "member", "role": "member", "tenant_id": "demo"}
    if method == "get":
        response = _get(db_conn, path, **who)
    elif method == "delete":
        response = _delete(db_conn, path, **who)
    else:
        response = _post(db_conn, path, {}, **who)

    assert response.status_code == 403, f"{method.upper()} {path}"


def test_sources_are_scoped_to_the_tenant(db_conn, goods_db):
    """别的租户的数据源不出现在这个租户的列表里。"""
    _create_source(db_conn, goods_db, tenant="other")

    assert _get(db_conn, "/api/admin/demo/db-import/sources").json()["items"] == []
    assert len(_get(db_conn, "/api/admin/other/db-import/sources").json()["items"]) == 1


def test_a_validation_error_response_does_not_echo_the_password(db_conn, goods_db):
    """校验失败的 422 响应里不能出现密码。

    pydantic v2 + FastAPI 的默认 422 会把每个错误对应的 `input` 原样回显——
    对请求体级别的错误（比如 extra="forbid" 拒掉的那个 password 字段），
    `input` 就是**整个请求体**，密码就在里面。它会被写进任何一层访问/错误
    日志，也会被截图。
    """
    response = _create_source(db_conn, goods_db, extra={"password": "hunter2"})

    assert response.status_code in (400, 422)
    assert "hunter2" not in response.text


def test_a_type_error_in_the_connection_fields_does_not_echo_the_password(db_conn):
    """连接字段类型不对（端口写成字母）时的 422 同样不能带密码。"""
    response = _post(
        db_conn, "/api/admin/demo/db-import/test-connection",
        {
            "driver": "mysql", "host": "h", "port": "not-a-number",
            "database": "d", "username": "u", "password": "hunter2",
        },
    )

    assert response.status_code == 422
    assert "hunter2" not in response.text


def test_other_routes_keep_the_default_validation_echo(db_conn):
    """脱敏只作用于数据库导入这一组。

    别的路由的 422 回显 input 是有用的调试信息，而且不含密码——全局一刀切
    的实现也能让上面两条变绿，但会让所有接口的校验错误变得难排查。
    """
    client, headers = _client(db_conn)
    try:
        response = client.post(
            "/api/admin/demo/diagnostics/not-a-number", json={}, headers=headers
        )
    finally:
        app.dependency_overrides.clear()

    # 405 或 422 都可能；只要是校验错误就得带 input。
    if response.status_code == 422:
        assert any("input" in error for error in response.json()["detail"])


def test_an_etl_failure_during_sync_is_a_400_with_the_reason_not_a_500(
    db_conn, goods_db, monkeypatch
):
    """同步跑到 ETL 那一步炸了，要把原因说出来，不是一个 500。

    500 的响应体是「Internal Server Error」——用户知道失败了，但不知道是
    本体没确认、安全阀拦下来了、还是图谱挂了，三件事要做的完全不同。
    真的让 run_schema_etl 抛（monkeypatch），不靠"配一个坏映射"——坏映射
    只会被当成跳过的 mapping，根本不抛。
    """
    from app.api import admin_db_import_routes

    async def _boom(**kwargs):
        raise RuntimeError("图谱在收尾阶段挂了")

    monkeypatch.setattr(admin_db_import_routes, "run_schema_etl", _boom)
    source_id = _create_source(db_conn, goods_db).json()["source_id"]

    response = _post(
        db_conn, f"/api/admin/demo/db-import/sources/{source_id}/sync", {"password": ""}
    )

    assert response.status_code == 400, response.text
    assert "图谱在收尾阶段挂了" in response.text
    # 失败的同步不刷新时间戳。
    stored = asyncio.run(get_db_source(db_conn, tenant_id="demo", source_id=source_id))
    assert stored is not None and stored["last_sync_at"] is None
