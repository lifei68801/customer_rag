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
    """按 (relation_type, from, to) 报告扇出度（单个主语最多连到几个宾语，
    不是边的总条数）的假图客户端，供
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


def test_deleting_every_question_means_none_not_auto_generated_ones(personas_conn):
    """把引导问题全删掉再保存，就是「一条都不要」，不是「回到自动兜底」。

    `[] or generate_questions(...)` 会让空列表落进兜底那一档——管理员刚
    删掉的内容对终端用户又冒出来，而且跟手写的长得一模一样。他唯一的
    出路会变成「留一条自己不想要的问题」，「能纠正」这半边完全不成立。

    本体这边同时具备"生成得出问题"的条件（组合已确认、图里真有边），
    否则「兜底也生成不出东西」会让这条用例在错误实现下照样绿。
    """
    asyncio.run(_add_relation_combo(personas_conn, "muji-goods", "产品", "RELATED_TO", "口味"))
    graph = FakeGraph({("RELATED_TO", "产品", "口味"): 9})
    # 先确认这个租户确实生成得出东西——不确认的话下面的 == [] 说明不了问题。
    assert _get_persona(personas_conn, graph=graph).json()["questions"] == ["产品有哪些口味？"]

    saved = _put_persona(personas_conn, questions=["Beer 是什么？"])
    assert saved.status_code == 200, saved.text
    cleared = _put_persona(personas_conn, questions=[])
    assert cleared.status_code == 200, cleared.text

    resp = _get_persona(personas_conn, graph=graph)
    assert resp.status_code == 200, resp.text
    assert resp.json()["questions"] == []
    assert resp.json()["questions_source"] == "handwritten"


def test_setting_only_the_face_still_leaves_generation_on(personas_conn):
    """只配过头像、从没碰过引导问题的租户，仍然走自动兜底。

    upsert_persona 会为这个租户插一行，questions 列取建表默认值——如果
    「有行」被当成「配过了」，配一次头像就会把自动兜底永久关掉，而界面
    上没有任何地方说过这件事。
    """
    asyncio.run(_add_relation_combo(personas_conn, "muji-goods", "产品", "RELATED_TO", "口味"))

    async def _face_only():
        await upsert_persona(
            personas_conn, tenant_id="muji-goods", avatar="🛍️", tagline="只配了脸"
        )

    asyncio.run(_face_only())

    graph = FakeGraph({("RELATED_TO", "产品", "口味"): 9})
    resp = _get_persona(personas_conn, graph=graph)
    assert resp.status_code == 200, resp.text
    assert resp.json()["questions"] == ["产品有哪些口味？"]
    assert resp.json()["questions_source"] == "generated"


def test_a_broken_graph_is_reported_as_unavailable_not_as_no_questions(personas_conn):
    """图谱连不上时，回包要说「查不了」，不是装成「本体里没什么可问的」。

    对终端用户两者都是空引导区（那是诚实的）；对管理员完全不同——后者是
    正常状态，前者是他该去修的故障，而他是唯一修得了的人。

    本体里放着一个本来生成得出问题的组合：不放的话，「图谱坏了」和「本体
    里本来就没组合」两条路径都返回空列表，用例分不出来。
    """
    asyncio.run(_add_relation_combo(personas_conn, "muji-goods", "产品", "RELATED_TO", "口味"))

    class BrokenGraph:
        async def probe_relation_fanout(self, **_: object) -> int:
            raise RuntimeError("Neo4j 连不上")

    resp = _get_persona(personas_conn, graph=BrokenGraph())
    assert resp.status_code == 200, resp.text
    assert resp.json()["questions"] == []
    assert resp.json()["questions_source"] == "unavailable"


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


# ---- 一个租户多张脸（ADR-0004）----
#
# persona_id 只用来定位一张脸，**绝不进任何权限判据**。下面的两条安全用例
# 守的正是"多脸不能成为绕过授权的新路径"。


def _faces_call(
    conn: aiosqlite.Connection, method: str, path: str, *,
    username: str = "alice", role: str = "member", json_body=None,
):
    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_review_conn] = lambda: conn
    app.dependency_overrides[deps.get_graph_client] = lambda: FakeGraph()
    try:
        token = session_store.create_session(username=username, role=role, tenant_id=None)
        client = TestClient(app)
        return client.request(
            method, path, json=json_body, headers={"Authorization": f"Bearer {token}"}
        )
    finally:
        app.dependency_overrides.clear()


def _create_face(conn, tenant_id="muji-goods", persona_id="dianwu", name="店务老张", **who):
    return _faces_call(
        conn, "POST", f"/api/admin/{tenant_id}/persona/faces",
        json_body={"persona_id": persona_id, "name": name}, **who,
    )


def test_a_tenant_with_two_faces_lists_both(personas_conn):
    """右栏要列出两张，不是一张。

    今天列表按租户各出一项——多脸之后同一个租户要出两项，且各自的头像/
    一句话是自己的。
    """
    assert _create_face(personas_conn).status_code == 201

    body = _get_personas(personas_conn, username="alice", role="member")
    goods = [p for p in body["personas"] if p["tenant_id"] == "muji-goods"]

    assert [p["persona_id"] for p in goods] == ["default", "dianwu"]
    assert goods[1]["name"] == "店务老张"
    # default 那张沿用租户名——它是解耦之前那唯一的一张脸。
    assert goods[0]["name"] == "杂货"


def test_a_tenant_with_no_face_still_lists_one(personas_conn):
    """没配过脸的租户合成一张 default，name 退回租户名——今天的行为不变。

    不合成的话，管理员新建租户并授权之后用户看不见它。
    """
    body = _get_personas(personas_conn, username="alice", role="member")
    store = [p for p in body["personas"] if p["tenant_id"] == "muji-store"]

    assert [(p["persona_id"], p["name"]) for p in store] == [("default", "门店")]


def test_building_a_named_face_first_does_not_hide_the_default_one(personas_conn):
    """租户从没配过脸、上来就建一张具名脸：default 仍然在，且排第一。

    default 不一定物化成一行；只在"一张都没有"时合成它的实现会在这里
    让它蒸发——不是被删，是从没合成过——而存量会话全挂在它下面。
    这条同时钉住列表接口和 /persona/faces 两个出口。
    """
    assert _create_face(
        personas_conn, tenant_id="muji-store", persona_id="kefu", name="客服",
    ).status_code == 201

    body = _get_personas(personas_conn, username="alice", role="member")
    store = [p for p in body["personas"] if p["tenant_id"] == "muji-store"]
    assert [(p["persona_id"], p["name"]) for p in store] == [("default", "门店"), ("kefu", "客服")]

    faces = _faces_call(personas_conn, "GET", "/api/admin/muji-store/persona/faces").json()["faces"]
    assert [f["persona_id"] for f in faces] == ["default", "kefu"]


def test_faces_of_an_unauthorized_tenant_do_not_appear(personas_conn):
    """**安全核心**：多脸不能成为绕过授权的新路径。

    secret 租户建了两张脸，alice 无权访问它——两张都不能出现在她的列表里。
    只按租户过滤、再把所有脸不加区分地拼进去的实现会在这里红。
    """
    assert _create_face(
        personas_conn, tenant_id="secret", persona_id="x", name="X",
        username="root", role="admin",
    ).status_code == 201

    body = _get_personas(personas_conn, username="alice", role="member")

    assert all(p["tenant_id"] != "secret" for p in body["personas"])


def test_writing_a_face_of_an_unauthorized_tenant_is_refused(personas_conn):
    """读挡住了不等于写挡住了，两条路径各断言一次。"""
    assert _create_face(personas_conn, tenant_id="secret").status_code == 403
    assert _faces_call(
        personas_conn, "DELETE", "/api/admin/secret/persona/faces/x"
    ).status_code == 403


def test_reading_and_writing_a_named_face(personas_conn):
    """按 persona_id 读写：两张脸的头像/一句话/引导问题互不干扰。"""
    _create_face(personas_conn)

    put = _faces_call(
        personas_conn, "PUT", "/api/admin/muji-goods/persona?persona_id=dianwu",
        json_body={"avatar": "B", "tagline": "我懂门店", "questions": ["产品有哪些？"]},
    )
    assert put.status_code == 200, put.text

    dianwu = _faces_call(
        personas_conn, "GET", "/api/admin/muji-goods/persona?persona_id=dianwu"
    ).json()
    default = _faces_call(personas_conn, "GET", "/api/admin/muji-goods/persona").json()
    assert (dianwu["persona_id"], dianwu["tagline"]) == ("dianwu", "我懂门店")
    assert default["tagline"] == "欢迎光临杂货部", "default 那张脸不能被另一张的写入碰到"


def test_deleting_the_default_face_is_refused(personas_conn):
    """default 那张脸删不掉。

    存量会话都挂在它下面（chat_sessions.persona_id 回填成 'default'）。
    删掉它，那些会话在右栏就没有归属，左栏整个空掉，而用户会以为历史丢了。
    """
    _create_face(personas_conn)

    resp = _faces_call(personas_conn, "DELETE", "/api/admin/muji-goods/persona/faces/default")

    assert resp.status_code == 409
    assert "default" in resp.text


def test_deleting_a_named_face_removes_it(personas_conn):
    _create_face(personas_conn)

    assert _faces_call(
        personas_conn, "DELETE", "/api/admin/muji-goods/persona/faces/dianwu"
    ).status_code == 200

    body = _get_personas(personas_conn, username="alice", role="member")
    goods = [p["persona_id"] for p in body["personas"] if p["tenant_id"] == "muji-goods"]
    assert goods == ["default"]


def test_a_blank_face_id_is_refused_at_the_http_layer(personas_conn):
    """空 persona_id 在接口这一层就挡住并说清楚，不是冲成 500。"""
    resp = _create_face(personas_conn, persona_id="   ")

    assert resp.status_code == 400
    assert "数字人 ID" in resp.text
