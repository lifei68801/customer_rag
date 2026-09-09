"""GET /api/admin/personas ——「我这个账号能访问哪些数字人」。

这是一个非租户路径（路径里没有 {tenant_id}），也是这个账号体系里少数
几个不挂 require_admin_role 的端点之一：右栏对所有角色都要出现，安全性
完全靠返回内容按 list_accessible_tenant_ids 过滤。
"""
from __future__ import annotations

import asyncio

import aiosqlite
import pytest
from fastapi.testclient import TestClient

from app.api import deps
from app.api.admin_session import AdminSessionStore
from app.auth.admin_users_store import create_admin_user
from app.auth.user_tenants_store import grant_tenant_access
from app.graphrag.ontology_categories import create_term_type
from app.graphrag.ontology_constraints import add_allowed_combination
from app.graphrag.ontology_lifecycle import checkout_draft, confirm_ontology, ensure_ontology_schema
from app.graphrag.tenant_personas_store import (
    ensure_tenant_personas_schema,
    upsert_persona,
)
from app.graphrag.tenants_store import create_tenant, create_tenants_table, set_tenant_status
from app.graphrag.term_edits_store import ensure_term_edits_schema
from app.graphrag.terms_store import create_term, ensure_terms_schema
from app.main import app
from tests.schema_fixtures import ensure_admin_auth_schema
from tests.settings_factory import build_settings


def _settings(**overrides):
    return build_settings(**{"admin_token": "tok", **overrides})


async def _open_personas_conn() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    await ensure_admin_auth_schema(conn)
    await create_tenants_table(conn)
    await ensure_tenant_personas_schema(conn)

    # 本任务（引导问题读写端点）新增：find_unmatched_questions 要有一份
    # 真实的合并视图（terms + term_edits）可读，generate_questions 要有一份
    # 真实的本体（ontology_*）可读。
    await ensure_terms_schema(conn)
    await ensure_term_edits_schema(conn)
    await ensure_ontology_schema(conn)

    await create_admin_user(conn, username="root", password="password1", role="admin", tenant_id=None)
    # admin_users 上的 CHECK 约束要求 member 必须有非空 tenant_id（这一列是
    # 存量回退值，见 user_tenants_store.py 顶部注释）。alice 在 user_tenants
    # 里有显式授权，这一列的具体取值不影响本文件任何用例的判定。
    await create_admin_user(conn, username="alice", password="password1", role="member", tenant_id="muji-goods")

    # 三个租户：muji-goods、muji-store 都建了 persona，secret 也建了但 alice
    # 无权访问——它必须**完全不出现**在 alice 的结果里，多列一个都是越权
    # （即使点进去会被 403 挡住，名字本身已经泄露"这家公司还有一个叫机密的
    # 领域"）。muji-store 故意不配脸，用来钉住"没配脸的租户仍要出现，
    # 只是 avatar/tagline 为空、name 退回租户名"。
    for tenant_id, name in (("muji-goods", "杂货"), ("muji-store", "门店"), ("secret", "机密")):
        await create_tenant(conn, tenant_id=tenant_id, name=name)

    # alice 只被授权前两个，secret 对她不可见。
    await grant_tenant_access(conn, username="alice", tenant_id="muji-goods")
    await grant_tenant_access(conn, username="alice", tenant_id="muji-store")

    await upsert_persona(conn, tenant_id="muji-goods", avatar="https://x/goods.png", tagline="欢迎光临杂货部")
    await upsert_persona(conn, tenant_id="secret", avatar="https://x/secret.png", tagline="机密频道")
    # muji-store 故意不调用 upsert_persona。

    # muji-goods 下一个已确认的实体类型「产品」+ 一条术语「Beer」——
    # find_unmatched_questions 的判据是"提到至少一个已知名字/别名/类型名"，
    # 下面几组用例（引导问题读写端点）用它构造"匹配"（提到 Beer 或"产品"）
    # 与"不匹配"（比如"库存"，本体里压根没有这个类型或术语）的问题。
    await create_term_type(conn, tenant_id="muji-goods", value="产品", actor="alice")
    await confirm_ontology(conn, "muji-goods", actor="alice")
    await create_term(
        conn, tenant_id="muji-goods", standard_name="Beer", aliases=[], term_type="产品",
    )

    return conn


@pytest.fixture
def personas_conn():
    """独立的 :memory: 连接，每个用例都拿到全新的种子数据（路由层依赖的是
    deps.get_review_conn，这里用 dependency_overrides 整个替换掉）。"""
    conn = asyncio.run(_open_personas_conn())
    try:
        yield conn
    finally:
        asyncio.run(conn.close())


def _get_personas_raw(conn: aiosqlite.Connection, *, headers: dict[str, str]):
    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_review_conn] = lambda: conn
    try:
        client = TestClient(app)
        return client.get("/api/admin/personas", headers=headers)
    finally:
        app.dependency_overrides.clear()


def _get_personas(conn: aiosqlite.Connection, *, username: str, role: str):
    """以指定账号登录后请求 /api/admin/personas，返回响应体（已断言 200）。"""
    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_review_conn] = lambda: conn
    try:
        token = session_store.create_session(username=username, role=role, tenant_id=None)
        client = TestClient(app)
        response = client.get(
            "/api/admin/personas", headers={"Authorization": f"Bearer {token}"}
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200, response.text
    return response.json()


def test_member_sees_only_the_personas_they_were_granted(personas_conn):
    """右栏只列这个账号有权访问的。多列一个都是越权——即使他点进去会被
    403 挡住，那个名字本身就已经泄露了「这家公司还有一个叫供应链的领域」。"""
    body = _get_personas(personas_conn, username="alice", role="member")
    assert [p["tenant_id"] for p in body["personas"]] == ["muji-goods", "muji-store"]


def test_admin_sees_every_active_tenant(personas_conn):
    """admin 不设限——它得能进入自己刚新建的租户。"""
    body = _get_personas(personas_conn, username="root", role="admin")
    assert [p["tenant_id"] for p in body["personas"]] == ["muji-goods", "muji-store", "secret"]


def test_disabled_tenants_do_not_appear(personas_conn):
    """停用的租户不列：切过去之后所有写操作都是 404，而用户不知道为什么。
    这一条钉的是「授权还在但租户停用了」这个组合。"""
    asyncio.run(set_tenant_status(personas_conn, "muji-store", "disabled"))
    body = _get_personas(personas_conn, username="alice", role="member")
    assert [p["tenant_id"] for p in body["personas"]] == ["muji-goods"]


def test_a_tenant_without_a_persona_still_appears_with_its_tenant_name(personas_conn):
    """没配过脸的租户照样出现在右栏，只是 avatar/tagline 为空。
    不出现的话，管理员新建一个租户、授权给用户，用户却看不见它——
    而没有任何地方告诉他「你需要先去配一张脸」。"""
    body = _get_personas(personas_conn, username="alice", role="member")
    unconfigured = next(p for p in body["personas"] if p["tenant_id"] == "muji-store")
    assert unconfigured["avatar"] == ""
    assert unconfigured["tagline"] == ""
    assert unconfigured["name"] == "门店"  # 退回租户名，不是空字符串
    # 顺带确认配了脸的那个不会被这条断言误判成"也是空的"。
    configured = next(p for p in body["personas"] if p["tenant_id"] == "muji-goods")
    assert configured["avatar"] == "https://x/goods.png"
    assert configured["tagline"] == "欢迎光临杂货部"


def test_anonymous_request_is_refused(personas_conn):
    """不带会话的请求得 401。右栏列的是「你能访问哪些知识库」，
    这个问题对匿名者没有答案。"""
    assert _get_personas_raw(personas_conn, headers={}).status_code == 401


# ---------------------------------------------------------------------------
# GET/PUT /api/admin/{tenant_id}/persona + GET .../persona/stale-questions
#
# 「当前这一个数字人」的详情、写入与失效检测。跟上面的 /api/admin/personas
# （非租户）完全分开：那条永远不带 questions（N+1 图查询的说明见
# admin_personas_routes.py::persona_router），这三条只处理当前这一个租户，
# 所以每次请求最多跑一次 generate_questions。
# ---------------------------------------------------------------------------


class FakeGraph:
    """按 (relation_type, from, to) 报告边数的假图客户端，供
    generate_questions 探测扇出度用。没登记的组合返回 0——"图里没有这种边"
    正是它要模拟的状态（同款写法见 tests/graphrag/test_guided_questions.py）。"""

    def __init__(self, fanouts: dict[tuple[str, str, str], int] | None = None) -> None:
        self._fanouts = fanouts or {}

    async def probe_relation_fanout(
        self, *, tenant_id: str, relation_type: str, from_term_type: str,
        to_term_type: str, direction: str,
    ) -> int:
        return self._fanouts.get((relation_type, from_term_type, to_term_type), 0)


async def _add_relation_combo(
    conn: aiosqlite.Connection, tenant_id: str, subject_type: str, relation_type: str, object_type: str
) -> None:
    """给 tenant_id 确认一条「主语类型 -关系-> 宾语类型」的允许组合，供
    generate_questions 使用。subject_type 假定已经是该租户下已确认的实体
    类型（本文件里固定复用 personas_conn 播种好的"产品"）——checkout_draft
    会把它原样复制进新草稿，这里只需要另外注册 object_type。"""
    await checkout_draft(conn, tenant_id)
    await create_term_type(conn, tenant_id=tenant_id, value=object_type, actor="alice")
    await add_allowed_combination(
        conn, tenant_id, subject_term_type=subject_type,
        relation_type=relation_type, object_term_type=object_type, actor="alice",
    )
    await confirm_ontology(conn, tenant_id, actor="alice")


def _get_persona(
    conn: aiosqlite.Connection, *, tenant_id: str = "muji-goods",
    username: str = "alice", role: str = "member", graph=None,
):
    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_review_conn] = lambda: conn
    # 必须显式覆盖：不覆盖的话 deps.get_graph_client 会尝试连接真实 Neo4j。
    app.dependency_overrides[deps.get_graph_client] = lambda: graph or FakeGraph()
    try:
        token = session_store.create_session(username=username, role=role, tenant_id=None)
        client = TestClient(app)
        return client.get(
            f"/api/admin/{tenant_id}/persona", headers={"Authorization": f"Bearer {token}"}
        )
    finally:
        app.dependency_overrides.clear()


def _put_persona(
    conn: aiosqlite.Connection, *, tenant_id: str = "muji-goods",
    username: str = "alice", role: str = "member",
    avatar: str = "https://x/face.png", tagline: str = "欢迎",
    questions: list[str] | None = None,
):
    if questions is None:
        questions = ["Beer 是什么？"]
    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_review_conn] = lambda: conn
    try:
        token = session_store.create_session(username=username, role=role, tenant_id=None)
        client = TestClient(app)
        return client.put(
            f"/api/admin/{tenant_id}/persona",
            json={"avatar": avatar, "tagline": tagline, "questions": questions},
            headers={"Authorization": f"Bearer {token}"},
        )
    finally:
        app.dependency_overrides.clear()


def _get_stale_questions(
    conn: aiosqlite.Connection, *, tenant_id: str = "muji-goods",
    username: str = "alice", role: str = "member",
):
    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_review_conn] = lambda: conn
    try:
        token = session_store.create_session(username=username, role=role, tenant_id=None)
        client = TestClient(app)
        return client.get(
            f"/api/admin/{tenant_id}/persona/stale-questions",
            headers={"Authorization": f"Bearer {token}"},
        )
    finally:
        app.dependency_overrides.clear()


def test_handwritten_questions_win_over_generated_ones(personas_conn):
    """手写优先。两档并列显示的话，用户分不清哪条是人写的——而这两者的
    可信度差很多（spec D2）。

    本体里同时具备"生成得出别的问题"的条件（产品-口味的组合，且图里确实
    有边），租户还手写保存了一条完全不同的问题（Beer 是什么？）——两档
    内容必须不同，否则「顺序反了」的实现也能变绿。"""
    asyncio.run(_add_relation_combo(personas_conn, "muji-goods", "产品", "RELATED_TO", "口味"))
    saved = _put_persona(personas_conn, questions=["Beer 是什么？"])
    assert saved.status_code == 200, saved.text

    graph = FakeGraph({("RELATED_TO", "产品", "口味"): 9})
    resp = _get_persona(personas_conn, graph=graph)
    assert resp.status_code == 200, resp.text
    assert resp.json()["questions"] == ["Beer 是什么？"]
    # 回包必须说清这一批是哪一种。编辑页照着渲染，分不清的话它会把自动
    # 兜底的那批显示成管理员自己配的，一按保存就固化成手写、从此不再随
    # 本体变化——而界面全程不说话。
    assert resp.json()["questions_source"] == "handwritten"


def test_generated_questions_fill_in_when_none_were_written(personas_conn):
    """一条手写都没有时才兜底。新数字人刚建好、还没人配问题时，
    前台不至于空着。"""
    asyncio.run(_add_relation_combo(personas_conn, "muji-goods", "产品", "RELATED_TO", "口味"))
    graph = FakeGraph({("RELATED_TO", "产品", "口味"): 9})
    resp = _get_persona(personas_conn, graph=graph)
    assert resp.status_code == 200, resp.text
    assert resp.json()["questions"] == ["产品有哪些口味？"]
    assert resp.json()["questions_source"] == "generated"


def test_saving_a_question_that_matches_nothing_is_refused_and_names_it(personas_conn):
    """保存被拒时必须点名是哪几条。只说「保存失败」的话，配了六条的人
    得自己一条条试出来是哪条有问题。"""
    body = _put_persona(personas_conn, questions=["Beer 是什么？", "库存多少？"])
    assert body.status_code == 400
    assert "库存多少？" in body.json()["detail"]
    assert "Beer 是什么？" not in body.json()["detail"]


def test_a_refused_save_writes_nothing(personas_conn):
    """校验不通过时一条都不存——存一半的话，界面上会显示一组用户从没
    确认过的问题。avatar/questions 两个字段都得钉住：只要有一个字段被
    部分写入了，就是"存一半"。"""
    good = _put_persona(personas_conn, avatar="https://x/original.png", questions=["Beer 是什么？"])
    assert good.status_code == 200, good.text

    bad = _put_persona(
        personas_conn, avatar="https://x/rejected.png",
        questions=["Beer 是什么？", "库存多少？"],
    )
    assert bad.status_code == 400

    after = _get_persona(personas_conn)
    body = after.json()
    assert body["avatar"] == "https://x/original.png"
    assert body["questions"] == ["Beer 是什么？"]


def test_saving_the_face_alone_does_not_clear_the_questions(personas_conn):
    """只改头像时 questions 不该被清空。"""
    first = _put_persona(personas_conn, avatar="https://x/face1.png", questions=["Beer 是什么？"])
    assert first.status_code == 200, first.text

    second = _put_persona(personas_conn, avatar="https://x/face2.png", questions=["Beer 是什么？"])
    assert second.status_code == 200, second.text

    after = _get_persona(personas_conn)
    body = after.json()
    assert body["avatar"] == "https://x/face2.png"
    assert body["questions"] == ["Beer 是什么？"]


def test_stale_questions_endpoint_reports_the_ones_that_stopped_matching(personas_conn):
    """本体改动导致某条手写问题不再命中时，它不是默默消失——看板要能
    问出来还剩几条失效的（spec 前台第二条硬规矩）。"""
    saved = _put_persona(personas_conn, questions=["Beer 是什么？"])
    assert saved.status_code == 200, saved.text

    async def _remove_beer():
        await personas_conn.execute(
            "DELETE FROM terms WHERE tenant_id = ? AND node_key = ?",
            ("muji-goods", "产品:Beer"),
        )
        await personas_conn.commit()

    asyncio.run(_remove_beer())

    resp = _get_stale_questions(personas_conn)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"stale": ["Beer 是什么？"]}


def test_a_member_cannot_write_another_tenants_persona(personas_conn):
    """写入端点必须走 require_tenant_access。读端点按 accessible 过滤，
    写端点却不校验的话，member 能改别人数字人的脸。"""
    assert _put_persona(personas_conn, tenant_id="secret", role="member").status_code == 403
